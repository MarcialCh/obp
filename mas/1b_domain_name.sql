SELECT 
	domain.name,
    save_circuit(provenance(), 'domain_id', '{path}') 
FROM 
	author, organization, writes, publication, conference, domain_conference, domain 
WHERE
	author.oid = organization.oid
	AND author.aid = writes.aid
    AND writes.pid = publication.pid
    AND publication.cid = conference.cid
    AND conference.cid = domain_conference.cid
    AND domain_conference.did = domain.did
    AND organization.name = 'University of California San Diego'
    AND publication.year > 2010
GROUP BY domain.name 
LIMIT 10