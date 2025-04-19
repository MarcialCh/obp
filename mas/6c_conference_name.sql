SELECT 
    conference.name,
    save_circuit(provenance(), 'conference_id', '{path}')
FROM 
    author as author1, author as author2, writes as writes1, writes as writes2, publication, conference 
WHERE
    author1.aid = writes1.aid
    AND writes1.pid = publication.pid
    AND author1.name = 'Tova Milo'
    AND author2.aid = writes2.aid
    AND writes2.pid = publication.pid
    AND author2.name = 'H. V. Jagadish'
    AND publication.cid = conference.cid
GROUP BY conference.name 
LIMIT 10