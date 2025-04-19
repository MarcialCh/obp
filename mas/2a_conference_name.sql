SELECT 
	conference.name,
    save_circuit(provenance(), 'conference_id', '{path}')
FROM 
	author, organization, writes, publication, conference 
WHERE
	author.oid = organization.oid
	AND author.aid = writes.aid
    AND writes.pid = publication.pid
    AND publication.cid = conference.cid
    AND organization.name = 'Tel Aviv University'
    AND publication.year > 2010
GROUP BY conference.name 
LIMIT 10