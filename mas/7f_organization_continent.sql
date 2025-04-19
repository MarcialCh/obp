SELECT 
    organization.continent,
    save_circuit(provenance(), 'organization_id', '{path}')
FROM 
    organization, author as author1, writes as writes1, author as author2, writes as writes2, publication, conference, domain_conference, domain 
WHERE
    organization.oid = author1.oid 
    AND organization.oid = author2.oid
    AND author1.name < author2.name
    AND author1.aid = writes1.aid
    AND author2.aid = writes2.aid
    AND writes1.pid = publication.pid
    AND writes2.pid = publication.pid
    AND publication.cid = conference.cid
    AND conference.cid = domain_conference.cid
    AND domain_conference.did = domain.did
    AND domain.name = 'Machine Learning & Pattern Recognition'
    and publication.year > 2010
GROUP BY organization.continent 
